"""Short-term memory (STM) reaper -- PHASE 1: expired POINTS only.

Runs IN-PROCESS against DatasetManager (the same storage layer the API
server uses), never through the REST surface.  Consequence, per
stmem/INTERFACE-FREEZE.md section 2: it needs NO dataset password.
_require_dataset_password is an API-boundary gate; DatasetManager's delete
paths take no password argument at all.  A dataset that somehow DOES carry a
password is SKIPPED and reported -- never bypassed (invariant 7).

WHAT THIS MODULE MAY AND MAY NOT DO
-----------------------------------
It deletes POINTS.  It never deletes a dataset.  stm_lifetime exposes
dataset_deletable() for the Phase 2 dataset lifecycle, and it is deliberately
not called from here: PLAN section 5 gates dataset deletion behind the backup
proof and the password decision, neither of which this module can assert.

HOW THE SAFETY INVARIANTS LAND HERE (PLAN section 7)
----------------------------------------------------
1. Existing data is untouchable.  Dataset selection goes through
   stm_lifetime.dataset_point_reapable(meta) and an ineligible dataset is
   skipped BEFORE ANY Qdrant call -- it is never even scrolled.  Every
   pre-STM dataset has no lifetime, so the reaper cannot reach its points by
   any path.
3. Never delete without a provable, server-stamped expiry.  The Qdrant Range
   filter on metadata.expires_at is the CANDIDATE selector only;
   stm_lifetime.reap_decision is the AUTHORITY and is re-checked for every
   point before any delete.  Anything unprovable is KEPT, and a malformed
   point is counted and skipped -- it can never abort the sweep.
4. The sweep always terminates.  Three independent guards: the cursor is
   tracked and a repeat stops the sweep, a per-dataset page cap bounds the
   loop, and an overall wall-clock budget defers the rest to the next tick.
5. A failed delete does not abandon the remaining deletes.  Each delete batch
   is attempted independently; a failure is recorded, the points stay in
   place, and the next batch (and the next sweep) proceeds.

Usage (the CronJob entry point)::

    python -m multimodal_rag.stm_reaper [--dataset NAME] [--dry-run] [--json]

--dry-run proves the SELECTION without deleting anything -- the recommended
first run against a live deployment.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from multimodal_rag import stm_lifetime as _lifetime

logger = logging.getLogger(__name__)

__all__ = [
    "BATCH_SIZE",
    "DEFAULT_MAX_BATCHES",
    "DEFAULT_SWEEP_BUDGET_S",
    "REAPER_NAME",
    "ReapReport",
    "ReapResult",
    "main",
    "reap_dataset",
    "reap_once",
]

REAPER_NAME = "stm-reaper"

#: Points collected per scroll page.  Mirrors the proven batch size in
#: DatasetManager.delete_session_history.
BATCH_SIZE = 100

#: Per-dataset pagination hard stop (invariant 4).  A healthy sweep needs one
#: page per BATCH_SIZE deleted points, so this is 100k points per dataset per
#: tick; a repeating cursor stops far earlier, at its first repeat.
DEFAULT_MAX_BATCHES = 1000

#: Wall-clock budget for one sweep.  The job must never outlive its own
#: schedule: whatever is left is picked up by the next tick.  0 disables.
DEFAULT_SWEEP_BUDGET_S = 600.0


# ---------------------------------------------------------------------------
# results
# ---------------------------------------------------------------------------


@dataclass
class ReapResult:
    """Per-dataset outcome.  Every field is a count or a stable token so the
    CronJob log line and the tests can assert on them directly."""

    dataset: str
    #: True when the dataset passed the Phase 1 gate and was actually scrolled.
    selected: bool = False
    #: Why the dataset was NOT swept (None when it was).  Stable tokens:
    #: no-meta / password-protected / not-point-reapable /
    #: collection-open-failed.
    skipped_reason: str | None = None
    #: Points the candidate filter returned and reap_decision re-checked.
    scanned: int = 0
    #: Points deleted (in dry-run: points that WOULD be deleted).
    reaped: int = 0
    #: Points the filter returned that the authority kept.
    kept: int = 0
    #: Subset of kept whose expiry was unprovable (invariant 3).
    malformed: int = 0
    #: Delete batches attempted / failed (invariant 5).
    delete_attempts: int = 0
    delete_failures: int = 0
    #: Scroll pages consumed.
    batches: int = 0
    #: The sweep stopped early (page cap, budget, or a repeating cursor).
    truncated: bool = False
    #: True when nothing was actually deleted (--dry-run).
    dry_run: bool = False
    #: Hard failures (scroll/delete).  These set the process exit code.
    errors: list[str] = field(default_factory=list)
    #: Soft notes (truncation, skip explanations).  Never fail the run.
    notes: list[str] = field(default_factory=list)

    def summary_line(self) -> str:
        parts = [
            f"dataset={self.dataset!r}",
            f"selected={self.selected}",
        ]
        if self.skipped_reason:
            parts.append(f"skipped={self.skipped_reason}")
        parts += [
            f"scanned={self.scanned}",
            f"reaped={self.reaped}",
            f"kept={self.kept}",
            f"malformed={self.malformed}",
            f"delete_failures={self.delete_failures}",
            f"batches={self.batches}",
            f"truncated={self.truncated}",
        ]
        if self.dry_run:
            parts.append("dry_run=True")
        return " ".join(parts)

    def to_dict(self) -> dict[str, Any]:
        return {
            "dataset": self.dataset,
            "selected": self.selected,
            "skipped_reason": self.skipped_reason,
            "scanned": self.scanned,
            "reaped": self.reaped,
            "kept": self.kept,
            "malformed": self.malformed,
            "delete_attempts": self.delete_attempts,
            "delete_failures": self.delete_failures,
            "batches": self.batches,
            "truncated": self.truncated,
            "dry_run": self.dry_run,
            "errors": list(self.errors),
            "notes": list(self.notes),
        }


@dataclass
class ReapReport:
    """Outcome of one full sweep (every eligible dataset)."""

    now: int = 0
    dry_run: bool = False
    results: list[ReapResult] = field(default_factory=list)
    #: Aggregated hard failures (dataset listing, scroll, delete).
    errors: list[str] = field(default_factory=list)

    def _sum(self, attr: str) -> int:
        return sum(int(getattr(r, attr, 0) or 0) for r in self.results)

    @property
    def datasets(self) -> int:
        return len(self.results)

    @property
    def swept(self) -> int:
        return sum(1 for r in self.results if r.selected)

    @property
    def skipped(self) -> int:
        return sum(1 for r in self.results if not r.selected)

    @property
    def scanned(self) -> int:
        return self._sum("scanned")

    @property
    def reaped(self) -> int:
        return self._sum("reaped")

    @property
    def kept(self) -> int:
        return self._sum("kept")

    @property
    def malformed(self) -> int:
        return self._sum("malformed")

    @property
    def delete_failures(self) -> int:
        return self._sum("delete_failures")

    @property
    def truncated(self) -> bool:
        return any(r.truncated for r in self.results)

    def summary_line(self) -> str:
        return (
            f"datasets={self.datasets} swept={self.swept} skipped={self.skipped} "
            f"scanned={self.scanned} reaped={self.reaped} kept={self.kept} "
            f"malformed={self.malformed} delete_failures={self.delete_failures} "
            f"truncated={self.truncated} errors={len(self.errors)}" + (" dry_run=True" if self.dry_run else "")
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "reaper": REAPER_NAME,
            "now": self.now,
            "dry_run": self.dry_run,
            "datasets": self.datasets,
            "swept": self.swept,
            "skipped": self.skipped,
            "scanned": self.scanned,
            "reaped": self.reaped,
            "kept": self.kept,
            "malformed": self.malformed,
            "delete_failures": self.delete_failures,
            "truncated": self.truncated,
            "errors": list(self.errors),
            "results": [r.to_dict() for r in self.results],
        }


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------


def _require_now(value: Any) -> int:
    """Validate the sweep clock ONCE, loudly.

    now is the reaper's single clock input (PLAN section 2.3).  A bad one is
    an operator/deployment error, not stored data, so it fails loud here
    instead of silently reaping against a nonsense deadline.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        # TRY004 suppressed -- see stm_lifetime.validate_lifetime: the STM
        # validators publish ONE exception type (ValueError) on purpose.
        raise ValueError(f"now must be an integer unix timestamp, got {type(value).__name__}")  # noqa: TRY004
    if value < 0:
        raise ValueError(f"now must be >= 0, got {value}")
    return value


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("true", "1", "yes", "on")


def _point_id(point: Any) -> Any:
    pid = getattr(point, "id", None)
    if pid is None and isinstance(point, Mapping):
        pid = point.get("id")
    return pid


def _point_payload(point: Any) -> Any:
    """Return the METADATA mapping the lifetime algebra reads.

    Real points are stored NESTED -- the writer builds
    PointStruct(payload={"page_content": ..., "metadata": doc.metadata})
    (rag_system.py) -- and every filter in this codebase keys on that nested
    path: "metadata.expires_at" (this module's candidate filter),
    "metadata.file_type" / "metadata.source" / "metadata.timestamp_start"
    (vector_store.py), "metadata.memory_kind" / "metadata.session_id"
    (dataset_manager.delete_session_history).

    reap_decision() reads expires_at from the mapping it is HANDED, so the
    authority must be handed the SAME mapping the candidate filter indexed:
    payload["metadata"].  Passing the outer payload instead would make the
    authority answer "no-expires-at" for every real point -- the filter would
    select candidates and the authority would keep all of them.  Fail-closed,
    but the feature would be silently inert.

    A flat payload (a legacy or hand-written shape) is passed through
    unchanged, so the function stays total.
    """
    payload = getattr(point, "payload", None)
    if payload is None and isinstance(point, Mapping):
        payload = point.get("payload")
    if isinstance(payload, Mapping):
        meta = payload.get("metadata")
        if isinstance(meta, Mapping):
            return meta
        # metadata present but not a mapping: fall through with the outer
        # payload -- reap_decision then finds no expiry and KEEPS the point.
    return payload


def _dataset_names(dm: Any) -> list[str]:
    """Every dataset name the manager knows about, in listing order.

    list_datasets() returns a plain list in its default (unpaginated) shape
    and {"datasets": [...], "next_cursor": ...} in its paginated one; both are
    accepted.  Raises on a listing failure so the caller can report it instead
    of silently reaping nothing.
    """
    datasets = dm.list_datasets()
    if isinstance(datasets, Mapping):
        datasets = datasets.get("datasets") or []
    names: list[str] = []
    for entry in datasets or []:
        name = entry.get("name") if isinstance(entry, Mapping) else getattr(entry, "name", None)
        if isinstance(name, str) and name:
            names.append(name)
    return names


def _read_meta(dm: Any, name: str) -> dict[str, Any] | None:
    """Dataset meta as stored, password hash included (read-only, fail-closed).

    The raw reader is preferred over get_dataset because the latter strips the
    password hash -- and the reaper's fail-closed skip must be able to SEE that
    a dataset is protected.  Nothing here is a bypass: a protected dataset is
    skipped, never opened.
    """
    reader = getattr(dm, "_read_meta", None)
    if callable(reader):
        meta = reader(name)
    else:
        try:
            meta = dm.get_dataset(name, sync_count=False)
        except Exception:
            return None
    return meta if isinstance(meta, dict) else None


def _is_password_protected(dm: Any, name: str, meta: Mapping[str, Any] | None) -> bool:
    """Invariant 7: a password-protected dataset is SKIPPED, never bypassed."""
    checker = getattr(dm, "has_password", None)
    if callable(checker):
        try:
            return bool(checker(name))
        except Exception:  # pragma: no cover - defensive: fall back to raw meta
            logger.debug("[%s] has_password(%r) failed; falling back to meta", REAPER_NAME, name, exc_info=True)
    return bool(isinstance(meta, Mapping) and meta.get("password_hash"))


def _open_collection(dm: Any, name: str) -> tuple[Any, str]:
    """Return (qdrant_client, collection_name) for *name*.

    check_embedder=False on purpose: deletion needs no embedder, and a
    fingerprint mismatch must not be able to block the reap.
    """
    rag = dm._get_rag(name, check_embedder=False)
    store = getattr(rag, "vector_store", None)
    if store is None or isinstance(store, dict):
        raise RuntimeError("dataset has no Qdrant vector store")
    client = getattr(store, "_client", None)
    collection = getattr(store, "collection_name", None)
    if client is None or not collection:
        raise RuntimeError("vector store is missing its client or collection name")
    return client, str(collection)


def _candidate_filter(now_ts: int) -> Any:
    """The CANDIDATE selector: a Qdrant numeric Range on the expiry stamp.

    Qdrant filters cannot do arithmetic (PLAN section 2.4), which is exactly
    why the server stamps expires_at at ingest.  A Range never matches a
    missing or null value, so points with no stamp -- every point that predates
    this feature -- are not even returned.
    """
    from qdrant_client.models import FieldCondition, Filter, Range

    return Filter(must=[FieldCondition(key="metadata.expires_at", range=Range(lt=now_ts))])


def _deadline_passed(deadline: float | None) -> bool:
    return deadline is not None and time.monotonic() >= deadline


def _delete_batch(client: Any, collection: str, ids: Sequence[Any], result: ReapResult, *, dry_run: bool) -> None:
    """Delete one batch.  NEVER raises (invariant 5).

    A failed batch is recorded and the caller keeps going: the points stay in
    the store and the next sweep retries them.
    """
    if not ids:
        return
    if dry_run:
        result.reaped += len(ids)
        return
    from qdrant_client.models import PointIdsList

    result.delete_attempts += 1
    try:
        client.delete(
            collection_name=collection,
            points_selector=PointIdsList(points=list(ids)),
            wait=True,
        )
    except Exception as exc:
        result.delete_failures += 1
        result.errors.append(f"delete of {len(ids)} point(s) failed: {exc!r}")
        logger.warning("[%s] %s: delete of %d point(s) failed: %s", REAPER_NAME, result.dataset, len(ids), exc)
        return
    result.reaped += len(ids)


# ---------------------------------------------------------------------------
# the reaper
# ---------------------------------------------------------------------------


def reap_dataset(
    dm: Any,
    name: str,
    now: int,
    *,
    batch_size: int = BATCH_SIZE,
    max_batches: int = DEFAULT_MAX_BATCHES,
    deadline: float | None = None,
    dry_run: bool = False,
) -> ReapResult:
    """Reap expired POINTS from ONE dataset.  Phase 1: never deletes a dataset.

    Returns a ReapResult; never raises for data-shaped problems (a malformed
    point is counted and kept, a failed delete is recorded and the sweep
    continues).
    """
    now_ts = _require_now(now)
    result = ReapResult(dataset=str(name), dry_run=dry_run)

    # -- the dataset gate, BEFORE any Qdrant traffic (invariant 1) ----------
    # An ineligible dataset is skipped WITHOUT being scrolled: this is what
    # makes existing data mathematically unreachable rather than merely
    # filtered.
    meta = _read_meta(dm, name)
    if meta is None:
        result.skipped_reason = "no-meta"
        result.notes.append(f"{name}: no readable meta.json -- skipped (fail-closed)")
        return result
    if _is_password_protected(dm, name, meta):
        result.skipped_reason = "password-protected"
        result.notes.append(
            f"{name}: password-protected -- skipped (invariant 7: the reaper never bypasses a password)"
        )
        return result
    if not _lifetime.dataset_point_reapable(meta):
        result.skipped_reason = "not-point-reapable"
        return result

    result.selected = True
    dataset_lifetime = meta.get("lifetime")

    try:
        client, collection = _open_collection(dm, name)
    except Exception as exc:
        result.selected = False
        result.skipped_reason = "collection-open-failed"
        result.errors.append(f"collection open failed: {exc!r}")
        return result

    scroll_filter = _candidate_filter(now_ts)
    seen_offsets: set[str] = set()
    offset: Any = None

    while True:
        # Invariant 4: two independent hard stops, plus the cursor guard below.
        if result.batches >= max_batches:
            result.truncated = True
            result.notes.append(f"{name}: page cap ({max_batches}) reached -- remainder deferred to the next sweep")
            break
        if _deadline_passed(deadline):
            result.truncated = True
            result.notes.append(f"{name}: sweep budget exhausted -- remainder deferred to the next sweep")
            break

        try:
            points, offset = client.scroll(
                collection,
                limit=batch_size,
                offset=offset,
                scroll_filter=scroll_filter,
                with_payload=True,
                with_vectors=False,
            )
        except Exception as exc:
            result.errors.append(f"scroll failed at offset {offset!r}: {exc!r}")
            logger.warning("[%s] %s: scroll failed at offset %r: %s", REAPER_NAME, name, offset, exc)
            break
        result.batches += 1

        if not points:
            break

        doomed: list[Any] = []
        for point in points:
            result.scanned += 1
            payload = _point_payload(point)
            try:
                verdict = _lifetime.reap_decision(payload, dataset_lifetime, now_ts)
            except Exception as exc:
                # reap_decision is TOTAL by contract (never raises); this is
                # belt-and-braces so one hostile point cannot abort the sweep
                # (invariant 3).
                result.kept += 1
                result.errors.append(f"point {_point_id(point)!r}: reap_decision raised {exc!r}")
                continue
            if verdict.reap:
                doomed.append(_point_id(point))
            else:
                result.kept += 1
                if verdict.reason == "malformed-expires-at":
                    result.malformed += 1
                logger.debug("[%s] %s: keep point %r (%s)", REAPER_NAME, name, _point_id(point), verdict.reason)

        _delete_batch(client, collection, doomed, result, dry_run=dry_run)

        if offset is None:
            break
        key = str(offset)
        if key in seen_offsets:
            # A repeating cursor would loop forever; the sweep must terminate
            # (invariant 4).  Whatever was already deleted stays deleted.
            result.truncated = True
            result.notes.append(f"{name}: repeating pagination offset {key!r} -- sweep stopped")
            break
        seen_offsets.add(key)

    if result.malformed:
        result.notes.append(f"{name}: {result.malformed} point(s) with an unprovable expiry were KEPT (invariant 3)")
    return result


def reap_once(
    dm: Any,
    now: int | None = None,
    *,
    dataset_names: Sequence[str] | None = None,
    batch_size: int = BATCH_SIZE,
    max_batches: int = DEFAULT_MAX_BATCHES,
    budget_s: float | None = DEFAULT_SWEEP_BUDGET_S,
    dry_run: bool = False,
) -> ReapReport:
    """Sweep every eligible dataset once.

    now defaults to the wall clock (the server's clock is the only clock this
    feature trusts).  dataset_names narrows the sweep -- used by the CronJob's
    optional --dataset and by tests.
    """
    now_ts = _require_now(int(time.time()) if now is None else now)
    report = ReapReport(now=now_ts, dry_run=dry_run)
    deadline = None if not budget_s or float(budget_s) <= 0 else time.monotonic() + float(budget_s)

    try:
        names = list(dataset_names) if dataset_names is not None else _dataset_names(dm)
    except Exception as exc:
        report.errors.append(f"dataset listing failed: {exc!r}")
        logger.error("[%s] dataset listing failed: %s", REAPER_NAME, exc)
        return report

    for name in names:
        if _deadline_passed(deadline):
            report.errors.append(
                f"sweep budget exhausted before {name!r} -- remaining datasets deferred to the next sweep"
            )
            break
        try:
            result = reap_dataset(
                dm,
                name,
                now_ts,
                batch_size=batch_size,
                max_batches=max_batches,
                deadline=deadline,
                dry_run=dry_run,
            )
        except Exception as exc:
            # One bad dataset must never abort the sweep (invariant 3, sweep
            # level -- the per-point guarantee is inside reap_dataset).
            report.errors.append(f"dataset {name!r}: unexpected {type(exc).__name__}: {exc}")
            logger.exception("[%s] dataset %r raised", REAPER_NAME, name)
            report.results.append(ReapResult(dataset=str(name), errors=[f"unexpected {type(exc).__name__}: {exc}"]))
            continue
        report.results.append(result)
        report.errors.extend(f"{name}: {err}" for err in result.errors)
        logger.info("[%s] %s", REAPER_NAME, result.summary_line())
        for note in result.notes:
            logger.warning("[%s] %s", REAPER_NAME, note)

    return report


# ---------------------------------------------------------------------------
# CronJob entry point
# ---------------------------------------------------------------------------


def _build_manager(data_path: str, qdrant_host: str, qdrant_port: int) -> Any:
    """Build the same DatasetManager the API server builds.

    Imported lazily so the safety-critical sweep logic stays importable (and
    unit-testable) without the model stack.  In-cluster the chart's ConfigMap
    and Secret supply MODEL_*_URL / MODEL_*_NAME via envFrom; the constructor
    verifies the embedder endpoint, exactly as the API server does at startup.
    """
    from multimodal_rag.dataset_manager import DatasetManager
    from multimodal_rag.model_config import build_all

    embedder, reranker, vlm, asr = build_all()
    if embedder is None:
        raise RuntimeError(
            "no embedder configured -- set MODEL_EMBEDDER_NAME / MODEL_EMBEDDER_URL "
            "(the reaper builds the same model pack as the API server)"
        )
    return DatasetManager(
        base_path=data_path,
        qdrant_host=qdrant_host,
        qdrant_port=qdrant_port,
        embedder=embedder,
        reranker=reranker,
        vlm=vlm,
        asr=asr,
        caption_with_asr=_env_flag("RAG_CAPTION_WITH_ASR"),
        caption_with_vlm=_env_flag("RAG_CAPTION_WITH_VLM"),
        remote=_env_flag("RAG_REMOTE", default=True),
        dedup_threshold=float(os.environ.get("RAG_DEDUP_THRESHOLD", "0.995")),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="stm_reaper",
        description=(
            "Short-term-memory reaper (Phase 1): delete expired POINTS from "
            "short-term datasets.  Never deletes a dataset."
        ),
    )
    parser.add_argument("--data-path", default=os.environ.get("DATA_PATH", "/data"))
    parser.add_argument("--qdrant-host", default=os.environ.get("QDRANT_HOST", ""))
    parser.add_argument("--qdrant-port", type=int, default=int(os.environ.get("QDRANT_PORT", "6333")))
    parser.add_argument("--batch-size", type=int, default=BATCH_SIZE)
    parser.add_argument("--max-batches", type=int, default=DEFAULT_MAX_BATCHES)
    parser.add_argument(
        "--budget-seconds",
        type=float,
        default=DEFAULT_SWEEP_BUDGET_S,
        help="wall-clock budget for the sweep (0 disables); the remainder is deferred to the next tick",
    )
    parser.add_argument(
        "--dataset",
        action="append",
        default=None,
        help="sweep only this dataset (repeatable; default: every dataset)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report what WOULD be deleted and delete nothing (recommended first run)",
    )
    parser.add_argument("--json", action="store_true", help="emit one JSON report line instead of the log summary")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    try:
        dm = _build_manager(args.data_path, args.qdrant_host, args.qdrant_port)
    except Exception as exc:
        logger.error("[%s] manager construction failed: %s", REAPER_NAME, exc)
        return 2

    report = reap_once(
        dm,
        dataset_names=args.dataset,
        batch_size=args.batch_size,
        max_batches=args.max_batches,
        budget_s=args.budget_seconds,
        dry_run=args.dry_run,
    )

    if args.json:
        print(json.dumps(report.to_dict(), sort_keys=True))
    logger.info("[%s] %s", REAPER_NAME, report.summary_line())
    for err in report.errors:
        logger.error("[%s] %s", REAPER_NAME, err)
    # A skip is not a failure, and a truncated sweep is by design.  Only real
    # scroll/delete failures (or a listing failure) are non-zero, so the
    # CronJob job history flags genuine trouble and nothing else.
    return 1 if report.errors else 0


if __name__ == "__main__":  # pragma: no cover - exercised by the CronJob
    raise SystemExit(main())
