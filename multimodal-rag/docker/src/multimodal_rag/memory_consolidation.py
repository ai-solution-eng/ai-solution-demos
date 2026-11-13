"""Memory lifecycle — decay scoring, archive planning, contradiction reporting.

The store can write memories and (since the 2026-11-10 wave) PATCH them, but
nothing ever decided what a memory is worth *later*.  A store that only grows
is a store whose recall gets worse: every search pays for the oldest, never-read
junk alongside the facts that are still true.  This module is the missing
lifecycle:

* **decay** — an importance score per record, multiplied by an exponential
  half-life, so age is a measurable quantity rather than a feeling;
* **archive** — the only destructive action, gated on three conditions at once
  (below the floor AND never accessed AND not a protected kind);
* **contradictions** — near-duplicate pairs whose advice points in opposite
  directions.  REPORTED, never resolved: two memories disagreeing is evidence
  about the world, and a lifecycle pass that silently picks one is a
  summarizer collapsing a real state.

Pure plan + thin apply
----------------------
plan_consolidation() is a pure function over record dicts — no clock, no I/O,
so the whole policy is testable with a fixed now.  apply_plan() is the thin
executor: it writes the decay scores back through
DatasetManager.update_memory_payload and deletes the archive set through
DatasetManager.delete_documents.  It refuses to delete a protected kind even
if a hand-edited plan asks for it, and a dry run is the DEFAULT.

Record shape accepted by the planner (raw Qdrant payloads and flat records
both work):

    {"memory_id": str,
     "kind": str,              # or "memory_kind"
     "text": str,              # or payload["page_content"]
     "memory_ts": str,         # ISO-8601; missing => age 0 (never archive an
                               # undated record on the strength of a guess)
     "access_count": int,      # optional; absent means never accessed
     "embedding": [float]}     # optional; absent falls back to lexical cosine
"""

from __future__ import annotations

import math
import re
import time
from datetime import UTC, datetime
from itertools import combinations
from typing import Any

#: Kinds a lifecycle pass must NEVER auto-archive.  A playbook's value is its
#: empirical counters: archiving the record destroys the evidence, so a
#: playbook is retired by a human or by the promotion gate, never by decay.
PROTECTED_KINDS = ("playbook",)

#: Importance weight per memory kind.  These order the *relative* worth of a
#: record type before access and age are applied; a kind not listed here uses
#: DEFAULT_KIND_WEIGHT.
KIND_WEIGHTS: dict[str, float] = {
    "playbook": 1.0,
    "decision": 0.85,
    "preference": 0.75,
    "project-summary": 0.6,
    "note": 0.5,
    "session_history": 0.35,
}
DEFAULT_KIND_WEIGHT = 0.5

#: Tunables.  Every key here is read by the planner; an unknown key passed in a
#: config dict is NOT silently accepted — it is echoed in
#: plan["stats"]["ignored_config_keys"] so a typo cannot masquerade as policy.
DEFAULT_CONFIG: dict[str, Any] = {
    "half_life_days": 30.0,
    "floor": 0.15,
    "contradiction_threshold": 0.92,
    "kind_weights": KIND_WEIGHTS,
    "default_kind_weight": DEFAULT_KIND_WEIGHT,
    "max_contradiction_pairs": 200,
    "max_records_for_pairs": 2000,
    "text_preview_chars": 160,
}

#: Opposing advice markers.  A near-duplicate pair only becomes a
#: contradiction when one record carries the first marker of a pair and the
#: other carries the second (and not vice versa) — "use X" vs "avoid X" is
#: opposite advice; "use X" vs "use Y" is merely different.
POLARITY_PAIRS: tuple[tuple[str, str], ...] = (
    ("always", "never"),
    ("enable", "disable"),
    ("use", "avoid"),
    ("required", "forbidden"),
    ("allow", "deny"),
    ("safe", "unsafe"),
    ("increase", "decrease"),
    ("keep", "remove"),
)

_TOKEN_RE = re.compile(r"[a-z0-9_]{2,}")
_WS_RE = re.compile(r"\s+")


# ---------------------------------------------------------------------------
# Coercion
# ---------------------------------------------------------------------------


def _text_of(record: dict[str, Any]) -> str:
    """The record body: page_content (raw payload) or text/preview (flat)."""
    for key in ("page_content", "text", "preview", "content"):
        value = record.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def _meta_of(record: dict[str, Any]) -> dict[str, Any]:
    """The structured fields: payload["metadata"] when present, else the record."""
    meta = record.get("metadata")
    if isinstance(meta, dict):
        merged = dict(record)
        merged.update(meta)
        return merged
    return record


def _kind_of(record: dict[str, Any]) -> str:
    meta = _meta_of(record)
    return str(meta.get("memory_kind") or meta.get("kind") or "note")


def _count_of(record: dict[str, Any]) -> int:
    meta = _meta_of(record)
    for key in ("access_count", "hits", "recall_count"):
        value = meta.get(key)
        if value is None:
            continue
        try:
            return max(0, int(value))
        except (TypeError, ValueError):
            continue
    return 0


def _timestamp_of(record: dict[str, Any]) -> float | None:
    meta = _meta_of(record)
    for key in ("memory_ts", "created_at", "last_validated", "ts"):
        value = meta.get(key)
        if value is None or value == "":
            continue
        if isinstance(value, (int, float)):
            return float(value)
        text = str(value).strip()
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.timestamp()
    return None


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(float(epoch), UTC).isoformat()


# ---------------------------------------------------------------------------
# Importance / decay
# ---------------------------------------------------------------------------


def _importance(
    kind: str,
    access_count: int,
    age_days: float,
    half_life_days: float,
    weights: dict[str, float],
    default_weight: float,
) -> float:
    """Importance = kind weight x access factor x a MILD recency boost.

    The access factor is 1 + log1p(access_count): one recall is worth more than
    none, the tenth is worth less than the second (a logarithm, so a
    frequently-recalled record cannot run away with the ranking).

    The recency boost is bounded to (0.75, 1.0] on purpose — it orders two
    otherwise-equal records by freshness, but it can never outweigh the
    exponential decay term below.  Age is therefore counted twice (once mildly
    here, once fully in the decay), which is deliberate: importance is the
    record's worth as of now, decayed is that worth after the half-life curve.
    """
    weight = float(weights.get(kind, default_weight))
    access = 1.0 + math.log1p(max(0, access_count))
    freshness = 0.75 + 0.25 * (2.0 ** (-max(0.0, age_days) / float(half_life_days)))
    return weight * access * freshness


def _decayed(importance: float, age_days: float, half_life_days: float) -> float:
    """importance * exp(-ln2 * age_days / half_life) — exact half-life semantics."""
    return importance * math.exp(-math.log(2.0) * max(0.0, age_days) / float(half_life_days))


# ---------------------------------------------------------------------------
# Contradiction detection
# ---------------------------------------------------------------------------


def _tokens(text: str) -> list[str]:
    return _TOKEN_RE.findall(_WS_RE.sub(" ", text.lower()))


def _lexical_cosine(a: str, b: str) -> float:
    """Cosine over token counts — the offline fallback when no embedding exists.

    A heuristic, and named as one: the result is REPORT-ONLY, so a false
    positive costs one line in a report, never a deletion.
    """
    counts_a: dict[str, int] = {}
    counts_b: dict[str, int] = {}
    for token in _tokens(a):
        counts_a[token] = counts_a.get(token, 0) + 1
    for token in _tokens(b):
        counts_b[token] = counts_b.get(token, 0) + 1
    if not counts_a or not counts_b:
        return 0.0
    dot = sum(count * counts_b.get(token, 0) for token, count in counts_a.items())
    norm_a = math.sqrt(sum(count * count for count in counts_a.values()))
    norm_b = math.sqrt(sum(count * count for count in counts_b.values()))
    if not norm_a or not norm_b:
        return 0.0
    return dot / (norm_a * norm_b)


def _embedding_cosine(a: Any, b: Any) -> float | None:
    if not isinstance(a, (list, tuple)) or not isinstance(b, (list, tuple)) or not a or not b:
        return None
    if len(a) != len(b):
        return None
    try:
        va = [float(x) for x in a]
        vb = [float(x) for x in b]
    except (TypeError, ValueError):
        return None
    dot = sum(x * y for x, y in zip(va, vb))
    norm_a = math.sqrt(sum(x * x for x in va))
    norm_b = math.sqrt(sum(y * y for y in vb))
    if not norm_a or not norm_b:
        return None
    return dot / (norm_a * norm_b)


def _markers(text: str) -> set[str]:
    lowered = _WS_RE.sub(" ", text.lower())
    found: set[str] = set()
    for left, right in POLARITY_PAIRS:
        for marker in (left, right):
            if re.search(rf"\b{re.escape(marker)}\b", lowered):
                found.add(marker)
    return found


def _opposing_polarity(text_a: str, text_b: str) -> tuple[str, str] | None:
    """The opposing marker pair carried by text_a / text_b, or None."""
    markers_a = _markers(text_a)
    markers_b = _markers(text_b)
    for left, right in POLARITY_PAIRS:
        if left in markers_a and right in markers_b and not (right in markers_a and left in markers_b):
            return (left, right)
        if right in markers_a and left in markers_b and not (left in markers_a and right in markers_b):
            return (right, left)
    return None


def _similarity(record_a: dict[str, Any], record_b: dict[str, Any]) -> float:
    numeric = _embedding_cosine(record_a.get("embedding"), record_b.get("embedding"))
    if numeric is not None:
        return numeric
    return _lexical_cosine(_text_of(record_a), _text_of(record_b))


def _contradictions(records: list[dict[str, Any]], cfg: dict[str, Any], stats: dict[str, Any]) -> list[dict]:
    threshold = float(cfg["contradiction_threshold"])
    cap = int(cfg["max_contradiction_pairs"])
    max_records = int(cfg["max_records_for_pairs"])
    if len(records) > max_records:
        stats["contradictions_skipped"] = f"more than {max_records} records"
        return []
    preview_chars = int(cfg["text_preview_chars"])
    found: list[dict] = []
    for record_a, record_b in combinations(records, 2):
        if len(found) >= cap:
            stats["contradictions_truncated"] = True
            break
        similarity = _similarity(record_a, record_b)
        if similarity < threshold:
            continue
        polarity = _opposing_polarity(_text_of(record_a), _text_of(record_b))
        if polarity is None:
            continue
        found.append(
            {
                "memory_id_a": record_a.get("memory_id"),
                "memory_id_b": record_b.get("memory_id"),
                "kind_a": record_a.get("kind"),
                "kind_b": record_b.get("kind"),
                "similarity": round(similarity, 4),
                "polarity": list(polarity),
                "text_a": _text_of(record_a)[:preview_chars],
                "text_b": _text_of(record_b)[:preview_chars],
                "action": "report-only",
            }
        )
    return found


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def _config(config: dict | None) -> tuple[dict[str, Any], list[str]]:
    """Resolved config plus the keys that were supplied but are not read."""
    cfg = dict(DEFAULT_CONFIG)
    ignored: list[str] = []
    if config is not None:
        if not isinstance(config, dict):
            raise ValueError("config must be a mapping of tunable name -> value")
        for key, value in config.items():
            if key in cfg:
                cfg[key] = value
            else:
                ignored.append(str(key))
    try:
        cfg["half_life_days"] = float(cfg["half_life_days"])
        cfg["floor"] = float(cfg["floor"])
        cfg["contradiction_threshold"] = float(cfg["contradiction_threshold"])
        cfg["default_kind_weight"] = float(cfg["default_kind_weight"])
        cfg["max_contradiction_pairs"] = int(cfg["max_contradiction_pairs"])
        cfg["max_records_for_pairs"] = int(cfg["max_records_for_pairs"])
        cfg["text_preview_chars"] = int(cfg["text_preview_chars"])
        cfg["kind_weights"] = {str(k): float(v) for k, v in dict(cfg["kind_weights"] or {}).items()}
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid consolidation config: {exc}") from exc
    if cfg["half_life_days"] <= 0:
        raise ValueError("half_life_days must be > 0")
    if cfg["floor"] < 0:
        raise ValueError("floor must be >= 0")
    if not 0.0 < cfg["contradiction_threshold"] <= 1.0:
        raise ValueError("contradiction_threshold must be in (0, 1]")
    return cfg, ignored


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------


def plan_consolidation(records: list[dict], *, now: float, config: dict | None = None) -> dict:
    """Decide what to decay, what to archive, and which memories disagree.

    Returns::

        {"decay": [...], "archive": [...], "contradictions": [...], "stats": {...}}

    * **decay** — records whose decayed importance fell below the floor but
      that must NOT be archived (a protected kind, or one that has been
      recalled at least once).  These are the records apply_plan() writes a
      decay score back for; the score is the audit trail that makes the next
      pass explainable.
    * **archive** — below the floor AND access_count == 0 AND kind not in
      PROTECTED_KINDS.  All three at once: age alone never deletes a memory —
      a record must also have NO access evidence at all, and protected kinds
      are exempt entirely.
    * **contradictions** — near-duplicate pairs (cosine >= threshold, from the
      stored embedding when present, else a lexical fallback) whose polarity
      markers oppose.  Report-only: never in the archive set.

    Records above the floor appear in no action list (they are counted in
    stats) — a lifecycle pass must not rewrite every payload just to say a
    memory is fine.
    """
    cfg, ignored = _config(config)
    now = float(now)
    half_life = cfg["half_life_days"]
    floor = cfg["floor"]
    weights = cfg["kind_weights"]
    default_weight = cfg["default_kind_weight"]

    normalized: list[dict[str, Any]] = []
    for record in records or []:
        if not isinstance(record, dict):
            continue
        normalized.append(
            {
                "memory_id": str(record.get("memory_id") or record.get("id") or ""),
                "kind": _kind_of(record),
                "text": _text_of(record),
                "ts": _timestamp_of(record),
                "access_count": _count_of(record),
                "embedding": record.get("embedding"),
                "raw": record,
            }
        )

    decay: list[dict] = []
    archive: list[dict] = []
    by_kind: dict[str, int] = {}
    protected_count = 0
    undated_count = 0
    preview_chars = cfg["text_preview_chars"]

    for entry in normalized:
        kind = entry["kind"]
        by_kind[kind] = by_kind.get(kind, 0) + 1
        ts = entry["ts"]
        if ts is None:
            undated_count += 1
            age_days = 0.0
        else:
            age_days = max(0.0, (now - ts) / 86400.0)
        importance = _importance(kind, entry["access_count"], age_days, half_life, weights, default_weight)
        decayed = _decayed(importance, age_days, half_life)
        protected = kind in PROTECTED_KINDS
        if protected:
            protected_count += 1
        if decayed >= floor:
            continue
        row = {
            "memory_id": entry["memory_id"],
            "kind": kind,
            "access_count": entry["access_count"],
            "age_days": round(age_days, 2),
            "dated": ts is not None,
            "importance": round(importance, 4),
            "decayed": round(decayed, 4),
            "protected": protected,
            "text_preview": entry["text"][:preview_chars],
        }
        if protected or entry["access_count"] > 0:
            row["action"] = "score"
            decay.append(row)
        else:
            row["action"] = "archive"
            archive.append(row)

    stats: dict[str, Any] = {}
    contradictions = _contradictions(normalized, cfg, stats)
    stats.update(
        {
            "total": len(normalized),
            "below_floor": len(decay) + len(archive),
            "above_floor": len(normalized) - len(decay) - len(archive),
            "archive": len(archive),
            "decay": len(decay),
            "protected": protected_count,
            "undated": undated_count,
            "contradictions": len(contradictions),
            "by_kind": by_kind,
            "half_life_days": half_life,
            "floor": floor,
            "contradiction_threshold": cfg["contradiction_threshold"],
            "now": _iso(now),
            "protected_kinds": list(PROTECTED_KINDS),
        }
    )
    if ignored:
        stats["ignored_config_keys"] = ignored
    return {"decay": decay, "archive": archive, "contradictions": contradictions, "stats": stats}


# ---------------------------------------------------------------------------
# Applying
# ---------------------------------------------------------------------------


def apply_plan(dm: Any, dataset: str, plan: dict, *, dry_run: bool = True) -> dict:
    """Execute a consolidation plan through a DatasetManager-like object.

    dry_run=True is the DEFAULT and performs no write: the result lists what
    would happen.  A real run deletes the archive set in one batched call and
    PATCHes a decay score onto each decay-list record via
    update_memory_payload (per record, because the score differs).

    Two independent guards, both deliberate:

    * a record whose kind is in PROTECTED_KINDS is never deleted, even if the
      plan (or a hand-edited JSON plan fed back to this function) says to —
      the protection lives in the executor, not only in the planner;
    * a per-record failure is collected in errors and does not abort the rest
      of the plan.  A lifecycle pass is not transactional: losing the whole
      archive batch because one score write failed is worse than a partial
      pass whose errors are reported.
    """
    archive_rows = [row for row in (plan or {}).get("archive") or [] if isinstance(row, dict)]
    decay_rows = [row for row in (plan or {}).get("decay") or [] if isinstance(row, dict)]
    protected_ids = [str(row.get("memory_id")) for row in archive_rows if row.get("kind") in PROTECTED_KINDS]
    protected_set = set(protected_ids)
    archive_ids = [str(row.get("memory_id")) for row in archive_rows if row.get("memory_id")]
    archive_ids = [memory_id for memory_id in archive_ids if memory_id not in protected_set]
    score_ids = [str(row.get("memory_id")) for row in decay_rows if row.get("memory_id")]
    checked_at = _iso(time.time())

    result: dict[str, Any] = {
        "applied": not dry_run,
        "dataset": dataset,
        "would_archive": archive_ids if dry_run else [],
        "would_score": score_ids if dry_run else [],
        "archived": 0,
        "scored": 0,
        "skipped_protected": protected_ids,
        "errors": [],
    }
    if dry_run:
        return result

    if archive_ids:
        try:
            result["archived"] = int(dm.delete_documents(dataset, archive_ids) or 0)
        except Exception as exc:  # reported, never fatal
            result["errors"].append(f"delete_documents: {exc}")

    floor = ((plan or {}).get("stats") or {}).get("floor")
    for row in decay_rows:
        memory_id = str(row.get("memory_id") or "")
        if not memory_id:
            continue
        patch: dict[str, Any] = {
            "decay_score": row.get("decayed"),
            "decay_checked_at": checked_at,
            "decay_action": "score",
        }
        if floor is not None:
            patch["decay_floor"] = floor
        try:
            touched = int(dm.update_memory_payload(dataset, [memory_id], patch) or 0)
        except Exception as exc:  # reported, never fatal
            result["errors"].append(f"update_memory_payload({memory_id}): {exc}")
            continue
        if touched:
            result["scored"] += 1
        else:
            result["errors"].append(f"update_memory_payload({memory_id}): no ids touched")
    return result
