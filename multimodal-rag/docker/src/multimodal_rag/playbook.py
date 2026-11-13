"""Playbook records — procedural memory for the playbook store.

A playbook is a *reusable procedure* tagged with the class of task it applies
to.  It is stored as an ordinary memory document with ``memory_kind =
"playbook"`` and structured fields in the payload metadata, so it can be
retrieved on demand (recall_playbooks) instead of being injected into every
session preamble.

Why this module is pure
-----------------------
Everything here is a function over already-fetched record dicts: no Qdrant, no
network, no clock.  The store lives in multimodal_rag.dataset_manager, the MCP
surface in multimodal_rag.mcp_server.  Keeping ranking, conflict detection and
rendering pure is what makes the empirical ordering testable without a cluster.

The record shape
----------------
A record is a flat dict (the same shape record_from_payload returns):

    {"memory_id": str,
     "playbook_key": str,            # sha256 of the normalized task signature
     "task_signature": str,
     "applies_when": [str],
     "steps": [str],
     "failure_modes": [str],
     "outcome": "success" | "abandoned",
     "outcome_source": "inferred",   # never "assessed": a model grading itself
                                     # is the failure mode this field exists to
                                     # avoid
     "success_count": int,
     "fail_count": int,
     "last_validated": str,          # ISO-8601
     "last_note": str}

Ranking is by EMPIRICAL evidence, not similarity: a playbook that worked six
times and failed once outranks a plausible-looking one that was never tried,
and an unproven record (0/0) always sorts last regardless of how similar it
looks.  The similarity filter belongs to the retrieval call (the MCP tool);
this module only orders what it is given.
"""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime
from typing import Any

#: ``metadata.memory_kind`` of a playbook memory document.  The store keys its
#: supersession rule on this value, so it is exported rather than inlined.
PLAYBOOK_MEMORY_KIND = "playbook"

#: Evidence labels attached by rank_playbooks().
PROVEN = "proven"
UNPROVEN = "unproven"

_SIGNATURE_WS = re.compile(r"\s+")


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------


def _normalize_signature(task_signature: Any) -> str:
    """Canonical form of a task signature: strip, lowercase, collapse whitespace.

    MUST stay byte-identical to the gateway overlay's implementation
    (integrations/pcai_llm_playbook/playbook.py): the distiller computes the
    ``playbook_key`` it writes, the store computes it again to decide what to
    supersede, and a normalization mismatch silently turns every re-emission
    into a new playbook with a fresh 0/0 counter set.
    """
    return _SIGNATURE_WS.sub(" ", str(task_signature or "").strip().lower())


def steps_fingerprint(steps: Any) -> str:
    """Stable identity of a PROCEDURE: sha256 over its normalized step list.

    playbook_key identifies the class of TASK; this identifies the procedure
    used to solve it.  Supersession needs BOTH (feature B cross-validation
    finding F10, 2026-11-10): two sessions can attack the same task signature
    with genuinely different procedures, and the design note requires both to
    be kept and surfaced as a disagreement.  Only a re-emission of the SAME
    procedure (same key AND same fingerprint) may replace and inherit.

    Normalization matches _normalized_steps: strip, lowercase, collapse every
    whitespace run, drop blank steps.  The steps are joined with a newline
    before hashing so ["a b", "c"] and ["a", "b c"] cannot collide.  An empty
    or missing step list hashes the empty string (never raises).
    """
    normalized = _normalized_steps(steps)
    return hashlib.sha256("\n".join(normalized).encode("utf-8")).hexdigest()


def playbook_key(task_signature: str) -> str:
    """Stable identity of a playbook's task class.

    sha256 (lowercase hex) of the normalized signature — see
    _normalize_signature.  Two sessions that solved the same class of task
    therefore share one key, which is what makes supersession and counter
    inheritance possible; an empty signature hashes the empty string (never
    raises).
    """
    return hashlib.sha256(_normalize_signature(task_signature).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Coercion helpers
# ---------------------------------------------------------------------------


def _count(value: Any) -> int:
    """Non-negative int from a stored counter; garbage reads as 0, never raises."""
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _parse_ts(value: Any) -> float | None:
    """Epoch seconds from an ISO-8601 string or a numeric epoch; None if unusable."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.timestamp()


def _recency_epoch(record: dict[str, Any], now: float | None) -> float:
    """Recency tie-break value: newest known timestamp, 0.0 when undated.

    ``now`` (when given) clamps future timestamps and lets a record that
    describes its age relatively (``age_days``, as consolidation plans do) be
    ordered against absolute ones.
    """
    for key in ("last_validated", "memory_ts", "created_at"):
        ts = _parse_ts(record.get(key))
        if ts is not None:
            return min(ts, now) if now is not None else ts
    age_days = record.get("age_days")
    if age_days is not None and now is not None:
        try:
            return float(now) - float(age_days) * 86400.0
        except (TypeError, ValueError):
            return 0.0
    return 0.0


def _normalized_steps(steps: Any) -> tuple[str, ...]:
    """Comparable form of a step list (strip/lower/collapse; blanks dropped)."""
    if not steps:
        return ()
    if isinstance(steps, str):
        steps = [steps]
    try:
        items = list(steps)
    except TypeError:
        return ()
    out: list[str] = []
    for step in items:
        norm = _SIGNATURE_WS.sub(" ", str(step).strip().lower())
        if norm:
            out.append(norm)
    return tuple(out)


# ---------------------------------------------------------------------------
# Ranking
# ---------------------------------------------------------------------------


def rank_playbooks(records: list[dict], *, now: float | None = None) -> list[dict]:
    """Order playbook records by empirical success, newest first among ties.

    Primary key: smoothed success rate ``(success + 1) / (success + fail + 2)``
    — Laplace smoothing, so 1/1 does not outrank 9/2 while still letting a
    record with a single success beat a never-tried one.  Unproven records
    (success + fail == 0) always sort LAST: their smoothed 0.5 is an artefact
    of the prior, not evidence.  Ties break on recency (newer ``last_validated``
    / ``memory_ts`` first), then on ``memory_id`` so the order is deterministic.

    Returns new dicts (the input records are not mutated) carrying two extra
    fields: ``score`` (the smoothed rate) and ``evidence`` ("proven" |
    "unproven").
    """
    ranked: list[dict[str, Any]] = []
    for record in records or []:
        if not isinstance(record, dict):
            continue
        row = dict(record)
        success = _count(row.get("success_count"))
        fail = _count(row.get("fail_count"))
        trials = success + fail
        row["success_count"] = success
        row["fail_count"] = fail
        row["score"] = round((success + 1.0) / (trials + 2.0), 6)
        row["evidence"] = PROVEN if trials else UNPROVEN
        row["_sort_key"] = (
            1 if not trials else 0,
            -float(row["score"]),
            -_recency_epoch(row, now),
            str(row.get("memory_id") or row.get("playbook_key") or ""),
        )
        ranked.append(row)
    ranked.sort(key=lambda row: row["_sort_key"])
    for row in ranked:
        row.pop("_sort_key", None)
    return ranked


# ---------------------------------------------------------------------------
# Conflict detection
# ---------------------------------------------------------------------------


def detect_conflicts(records: list[dict]) -> list[dict]:
    """Group records by ``playbook_key`` and report the ones with divergent steps.

    Two playbooks for the same task signature with different procedures is a
    REAL state, not an error: one may have worked while the other was
    abandoned.  This function never picks a winner — it reports the variants
    with their counts so the caller can show the disagreement.  Records sharing
    a key but carrying identical (normalized) steps are not a conflict; neither
    is a record with no steps at all (that is an incomplete record, not
    opposite advice).
    """
    groups: dict[str, list[dict]] = {}
    for record in records or []:
        if not isinstance(record, dict):
            continue
        key = str(record.get("playbook_key") or "").strip() or playbook_key(str(record.get("task_signature") or ""))
        groups.setdefault(key, []).append(record)

    conflicts: list[dict] = []
    for key, group in groups.items():
        if len(group) < 2:
            continue
        variants: list[dict] = []
        seen: set[tuple[str, ...]] = set()
        for record in rank_playbooks(group):
            steps = _normalized_steps(record.get("steps"))
            if not steps or steps in seen:
                continue
            seen.add(steps)
            variants.append(
                {
                    "memory_id": record.get("memory_id"),
                    "steps": list(record.get("steps") or []),
                    "outcome": record.get("outcome"),
                    "outcome_source": record.get("outcome_source"),
                    "success_count": _count(record.get("success_count")),
                    "fail_count": _count(record.get("fail_count")),
                    "evidence": record.get("evidence"),
                    "last_validated": record.get("last_validated"),
                }
            )
        if len(variants) < 2:
            continue
        conflicts.append(
            {
                "playbook_key": key,
                "task_signature": str(group[0].get("task_signature") or ""),
                "variants": variants,
                "count": len(group),
                "reason": "same task signature, divergent steps",
            }
        )
    return conflicts


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _steps_phrase(steps: Any, *, limit: int = 3, width: int = 120) -> str:
    """Short one-line rendering of a step list, for the disagreement line."""
    try:
        items = [str(step).strip() for step in (steps or []) if str(step).strip()]
    except TypeError:
        items = []
    if not items:
        return "(no steps)"
    phrase = " -> ".join(items[:limit])
    if len(items) > limit:
        phrase += " -> …"
    return phrase if len(phrase) <= width else phrase[: width - 1] + "…"


def format_recall(records: list[dict]) -> str:
    """Human-readable recall: the ranked playbooks plus an explicit disagreement line.

    The disagreement line is not decoration.  Two procedures for one task
    signature are a real state, and the design forbids a summarizer from
    collapsing them into one confident answer — so the rendering states the
    disagreement and each variant's empirical record instead of silently
    picking the top-ranked one.
    """
    ranked = rank_playbooks(records)
    if not ranked:
        return "No playbooks matched."

    lines = [f"{len(ranked)} playbook(s), ranked by empirical success rate:"]
    for index, record in enumerate(ranked, 1):
        success = record["success_count"]
        fail = record["fail_count"]
        rate = f"{success}/{fail}" if success + fail else "0/0 (unproven)"
        lines.append("")
        lines.append(
            f"{index}. {record.get('task_signature') or '(no task signature)'} — {record['evidence']} ({rate})"
        )
        applies_when = record.get("applies_when") or []
        if applies_when:
            lines.append(f"   applies when: {'; '.join(str(item) for item in applies_when)}")
        steps = record.get("steps") or []
        if steps:
            lines.append("   steps:")
            for number, step in enumerate(steps, 1):
                lines.append(f"     {number}. {step}")
        failure_modes = record.get("failure_modes") or []
        if failure_modes:
            lines.append(f"   failure modes: {'; '.join(str(item) for item in failure_modes)}")
        facts: list[str] = []
        if record.get("outcome"):
            facts.append(f"outcome: {record['outcome']} ({record.get('outcome_source') or 'unknown'})")
        if record.get("last_validated"):
            facts.append(f"last validated: {record['last_validated']}")
        if record.get("memory_id"):
            facts.append(f"memory_id: {record['memory_id']}")
        if facts:
            lines.append("   " + " | ".join(facts))

    conflicts = detect_conflicts(ranked)
    if conflicts:
        lines.append("")
        for conflict in conflicts:
            variants = conflict.get("variants") or []
            described = "; ".join(
                f'"{_steps_phrase(variant.get("steps"))}" ({variant["success_count"]}/{variant["fail_count"]})'
                for variant in variants
            )
            lines.append(
                f"DISAGREEMENT: {len(variants)} playbooks share the task signature "
                f'"{conflict["task_signature"]}" with divergent steps — {described}. '
                "Keep both: the empirical rate is the tie-break, and a summarizer must not "
                "collapse them into one answer."
            )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Supersession
# ---------------------------------------------------------------------------


def merge_counts(old: dict, new: dict) -> dict:
    """Lifecycle fields for a superseding playbook record: counters are inherited.

    Supersession replaces the *procedure*; it must never replace the
    *evidence*.  A re-emitted playbook arrives with 0/0 (a fresh emission has
    no history), so the store inherits the old totals instead of resetting
    them — otherwise "this worked 6 times, failed once" is erased every time
    the distiller improves the wording.

    ``max()`` per counter, deliberately: the inheritance is monotonic and
    IDEMPOTENT.  Re-applying it (a retried write, a re-emitted playbook that
    already carried inherited counts) cannot inflate the numbers, which a sum
    would.  ``last_validated`` takes the later ISO-8601 value and ``last_note``
    the newer record's note when it has one.  Keys with no value on either
    side are omitted, so the returned patch never nulls a stored field.
    """
    old = old if isinstance(old, dict) else {}
    new = new if isinstance(new, dict) else {}
    patch: dict[str, Any] = {
        "success_count": max(_count(old.get("success_count")), _count(new.get("success_count"))),
        "fail_count": max(_count(old.get("fail_count")), _count(new.get("fail_count"))),
    }
    validated = [
        str(value)
        for value in (new.get("last_validated"), old.get("last_validated"))
        if value is not None and str(value).strip()
    ]
    if validated:
        patch["last_validated"] = max(validated)
    note = new.get("last_note") or old.get("last_note")
    if note:
        patch["last_note"] = str(note)
    return patch


# ---------------------------------------------------------------------------
# Payload -> record
# ---------------------------------------------------------------------------


def record_from_payload(memory_id: str, payload: dict | None) -> dict:
    """Normalize a stored memory payload into a playbook record.

    Qdrant memory documents keep their structured fields under
    ``payload["metadata"]`` and the human-readable body under
    ``payload["page_content"]`` (see MultimodalRAG._to_documents).  Both flat
    records (already normalized) and raw payloads are accepted, so callers and
    tests can hand in either.

    Missing fields are filled with the empty/zero defaults — a malformed record
    is rendered as incomplete, never raised on.
    """
    payload = payload if isinstance(payload, dict) else {}
    meta = payload.get("metadata")
    if not isinstance(meta, dict):
        meta = payload
    record: dict[str, Any] = {"memory_id": str(memory_id)}
    for key in (
        "playbook_key",
        "task_signature",
        "outcome",
        "outcome_source",
        "last_validated",
        "last_note",
        "evidence_thread",
        "memory_ts",
    ):
        value = meta.get(key)
        if value is not None and value != "":
            record[key] = value
    for key in ("applies_when", "steps", "failure_modes"):
        value = meta.get(key)
        if value is None:
            value = []
        elif isinstance(value, str):
            value = [value]
        elif not isinstance(value, (list, tuple)):
            value = [str(value)]
        record[key] = list(value)
    record["success_count"] = _count(meta.get("success_count"))
    record["fail_count"] = _count(meta.get("fail_count"))
    body = payload.get("page_content")
    if isinstance(body, str) and body.strip():
        record["text"] = body
    if not record.get("playbook_key") and record.get("task_signature"):
        record["playbook_key"] = playbook_key(str(record["task_signature"]))
    return record
