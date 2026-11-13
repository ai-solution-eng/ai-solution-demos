"""Short-term memory (STM) lifetime algebra -- the safety crux of the STM feature.

PURE and dependency-free by design: no Qdrant, no I/O, no clock.  Callers pass
'now' / 'created_at' explicitly, so every branch is deterministically testable
and the reaper's selection predicate can be enumerated over REAL dataset
metadata in a unit test (PLAN section 7, invariant 1).

RATIFIED SEMANTICS (stmem/PLAN-short-term-memory.md section 2):

    effective_lifetime(point_lifetime, dataset_lifetime)
        treat None as INFINITY
        return min(point_lifetime, dataset_lifetime)

'None' means INFINITE.  Every dataset that predates this feature has no
'lifetime' field, and every point in it has no 'expires_at' field -- so
existing data is unreachable by the reaper TWICE OVER (dataset-level skip plus
point-level keep).  That is an arithmetic guarantee, not a filter we have to
remember to write.

THE TWO FAILURE DIRECTIONS ARE NOT SYMMETRIC
--------------------------------------------
* WRITE time (dataset create / point ingest): a malformed lifetime is a caller
  bug.  We RAISE -- silently accepting it would mint data whose lifetime nobody
  can reason about.  See 'validate_lifetime'.
* REAP time (reading stored payloads back): a malformed 'expires_at' is data we
  did not write.  We KEEP the point.  The reaper must never delete something it
  cannot PROVE is expired.  See 'reap_decision'.

ROW 5 OF THE TRUTH TABLE IS STORED BUT NOT ENFORCED (deliberate)
---------------------------------------------------------------
The truth table's row '(1h, None) -> 1h' says a point may cap itself inside an
immortal dataset.  'stamp_point_times' still writes that 'expires_at' (so the
value is truthful and auditable), but PLAN section 4.5 and invariant 1 both
require the reaper to SKIP a dataset with no 'lifetime' entirely -- so no sweep
acts on it while the dataset stays immortal.  If the dataset later gains a
lifetime, the stored 'expires_at' becomes actionable.  This is the conservative
reading of a ratified plan; see stmem/INTERFACE-FREEZE.md.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any, NamedTuple

__all__ = [
    "I64_MAX",
    "ReapDecision",
    "compute_expires_at",
    "dataset_deletable",
    "dataset_point_reapable",
    "effective_lifetime",
    "reap_decision",
    "stamp_point_times",
    "validate_lifetime",
    "validate_stored_lifetime",
]

#: Qdrant Range bounds are signed 64-bit.  An expires_at beyond this would not
#: round-trip through the filter, so it is refused at write time.
I64_MAX = 2**63 - 1


class ReapDecision(NamedTuple):
    """The reaper's verdict for ONE point.

    'reason' is a stable, machine-checkable token -- tests assert on it
    directly rather than on log text.
    """

    reap: bool
    reason: str
    expires_at: int | None


# ---------------------------------------------------------------------------
# write-time validation (fail LOUD)
# ---------------------------------------------------------------------------


def validate_lifetime(value: Any, *, field: str = "lifetime") -> int | None:
    """Return *value* as a positive int number of seconds, or None.

    None is the INFINITE sentinel and is always accepted.  Anything else must
    be a positive whole number of seconds.  Raises ValueError otherwise -- see
    the module docstring for why write time is strict while reap time is
    lenient.

    Rejects bool explicitly: isinstance(True, int) is True in Python, so an
    unguarded check would silently accept lifetime=True as one second.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        # TRY004 suppressed: a TypeError here would ESCAPE the
        # `except ValueError` in validate_stored_lifetime and break its
        # documented never-raises (fail-closed) guarantee. One exception type
        # is the contract.
        raise ValueError(f"{field} must be a positive integer number of seconds or None, got bool")  # noqa: TRY004
    if isinstance(value, float):
        if not math.isfinite(value) or not value.is_integer():
            raise ValueError(f"{field} must be a whole number of seconds, got {value!r}")
        value = int(value)
    if not isinstance(value, int):
        # TRY004 suppressed -- see the bool branch above: validate_stored_lifetime
        # catches ValueError only, so the type gate must stay ValueError.
        raise ValueError(  # noqa: TRY004
            f"{field} must be a positive integer number of seconds or None, got {type(value).__name__}"
        )
    if value <= 0:
        raise ValueError(f"{field} must be > 0 seconds (use None for infinite), got {value}")
    if value > I64_MAX:
        raise ValueError(f"{field} of {value} seconds exceeds the signed 64-bit range Qdrant can index")
    return value


def validate_stored_lifetime(value: Any, *, field: str = "lifetime") -> int | None:
    """Fail-closed variant of validate_lifetime for STORED values.

    Never raises.  A malformed stored lifetime reads as None (INFINITE), which
    makes every dependent decision KEEP rather than delete.
    """
    try:
        return validate_lifetime(value, field=field)
    except ValueError:
        return None


def _require_timestamp(value: Any, *, field: str) -> int:
    """Return *value* as a non-negative int unix timestamp.  Raises otherwise."""
    if isinstance(value, bool) or not isinstance(value, int):
        # TRY004 suppressed -- see validate_lifetime: one exception type is the
        # contract these validators publish.
        raise ValueError(f"{field} must be an integer unix timestamp, got {type(value).__name__}")  # noqa: TRY004
    if value < 0:
        raise ValueError(f"{field} must be >= 0, got {value}")
    if value > I64_MAX:
        raise ValueError(f"{field} {value} exceeds the signed 64-bit Qdrant Range bound")
    return value


def _as_finite_number(value: Any) -> int | float | None:
    """Best-effort numeric coercion for a STORED payload value.

    Accepts int/float and numeric strings (a numeric string is unambiguous, so
    treating it as malformed would leak points that a buggy writer stored as
    text).  Rejects bool, NaN, +/-inf, and everything else -- those are
    genuinely unknowable and must fail closed.

    THIS FUNCTION MUST NEVER RAISE.  It is called from 'reap_decision' on
    payloads this server did not write, and an exception there aborts the whole
    sweep -- turning a single malformed point into a fail-OPEN denial of the
    entire reap (PLAN section 7 invariant 3).  A huge Python int is the trap:
    float(10**400) raises OverflowError rather than returning inf, so ints are
    handled natively and range-checked instead of being round-tripped through
    float.  A non-integer float is likewise bounded to the i64 Range window.

    A value outside [1, I64_MAX] is MALFORMED (KEEP), not "infinitely far in
    the future": our writer stamps expires_at from a server clock plus a
    positive lifetime, so a legitimate value is always >= 1 and always within
    the signed 64-bit range Qdrant can index.  Anything else is data we cannot
    prove is expired, and failing closed is the whole point.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if 1 <= value <= I64_MAX else None
    if isinstance(value, float):
        if not math.isfinite(value) or not value.is_integer():
            return None
        return int(value) if 1 <= value <= I64_MAX else None
    if isinstance(value, str):
        try:
            number = float(value.strip())
        except (TypeError, ValueError, OverflowError):
            return None
        if not math.isfinite(number) or not number.is_integer():
            return None
        return int(number) if 1 <= number <= I64_MAX else None
    return None


# ---------------------------------------------------------------------------
# the algebra
# ---------------------------------------------------------------------------


def effective_lifetime(point_lifetime: Any, dataset_lifetime: Any) -> int | None:
    """Combine a point lifetime with its dataset's lifetime.

    min() of the two, with None meaning INFINITY.  The dataset lifetime is a
    HARD CAP: no point in a finite dataset can outlive it.  An earlier draft
    used max(); that was wrong and was corrected by the product owner.

    Raises ValueError on a malformed input (write-time semantics).
    """
    point = validate_lifetime(point_lifetime, field="point lifetime")
    dataset = validate_lifetime(dataset_lifetime, field="dataset lifetime")
    if point is None:
        return dataset
    if dataset is None:
        return point
    return min(point, dataset)


def compute_expires_at(created_at: Any, lifetime: Any) -> int | None:
    """Return created_at + lifetime, or None when the lifetime is infinite.

    *created_at* MUST be a server-stamped clock reading (PLAN section 2.3: a
    client-supplied timestamp lets a writer shorten or lengthen its own
    entries' lives, so it is never trusted).
    """
    created = _require_timestamp(created_at, field="created_at")
    life = validate_lifetime(lifetime)
    if life is None:
        return None
    expires = created + life
    if expires > I64_MAX:
        raise ValueError(f"expires_at {expires} overflows the signed 64-bit Qdrant Range bound")
    return expires


def stamp_point_times(
    *,
    now: Any,
    point_lifetime: Any = None,
    dataset_lifetime: Any = None,
) -> dict[str, Any]:
    """Server-stamp the three lifetime fields for an ingested point.

    Returns a dict ready to merge into a document's metadata.  'expires_at' is
    None for an immortal point; storing the explicit None (rather than omitting
    the key) keeps the payload self-describing, and a Qdrant numeric Range
    filter simply never matches a null value.
    """
    created = _require_timestamp(now, field="now")
    life = effective_lifetime(point_lifetime, dataset_lifetime)
    return {
        "lifetime": life,
        "created_at": created,
        "expires_at": compute_expires_at(created, life),
    }


# ---------------------------------------------------------------------------
# reap-time predicates (fail CLOSED)
# ---------------------------------------------------------------------------


def reap_decision(
    payload: Mapping[str, Any] | None,
    dataset_lifetime: Any,
    now: Any,
) -> ReapDecision:
    """Decide whether ONE point may be reaped.  Never raises; defaults to KEEP.

    The Qdrant filter is the CANDIDATE selector (cheap, server-side); this
    predicate is the AUTHORITY.  Every point the filter returns is re-checked
    here before any delete is issued, which is what makes invariant 1 provable
    by enumeration rather than by trusting a filter expression.

    Reasons, in evaluation order:

    no-payload            point carries no payload at all -- KEEP
    dataset-immortal      dataset has no (or malformed) lifetime -- KEEP
    no-expires-at         point has no expiry stamp -- KEEP (immortal point)
    malformed-expires-at  expiry is not a finite number -- KEEP
    expired               expires_at < now -- REAP
    not-expired           expires_at >= now -- KEEP
    """
    now_ts = _require_timestamp(now, field="now")

    if payload is None:
        return ReapDecision(False, "no-payload", None)

    # Invariant 1: a dataset with no lifetime is never selected by the reaper.
    # Checked BEFORE the point, so an immortal dataset short-circuits every
    # point in it regardless of what the point itself carries.
    if validate_stored_lifetime(dataset_lifetime, field="dataset lifetime") is None:
        return ReapDecision(False, "dataset-immortal", None)

    raw = payload.get("expires_at")
    if raw is None:
        return ReapDecision(False, "no-expires-at", None)

    number = _as_finite_number(raw)
    if number is None:
        return ReapDecision(False, "malformed-expires-at", None)

    expires = int(number)
    if expires < now_ts:
        return ReapDecision(True, "expired", expires)
    return ReapDecision(False, "not-expired", expires)


# ---------------------------------------------------------------------------
# dataset-level gates
# ---------------------------------------------------------------------------


def dataset_point_reapable(meta: Any) -> bool:
    """Phase 1 gate: may the POINT reaper sweep this dataset at all?

    True only when the dataset carries a valid, set lifetime.  A dataset with a
    missing / None / malformed lifetime is skipped ENTIRELY, so the sweep never
    even scrolls it (PLAN section 4.5).  Non-dict input is fail-closed (False).
    """
    if not isinstance(meta, Mapping):
        return False
    return validate_stored_lifetime(meta.get("lifetime"), field="dataset lifetime") is not None


def dataset_deletable(meta: Any) -> bool:
    """Phase 2 gate (invariant 6): may the reaper delete this DATASET itself?

    Requires BOTH kind == "short_term" AND a valid set lifetime.  A normal
    dataset is never deletable, whatever other fields it carries.  Non-dict
    input is fail-closed (False).
    """
    if not isinstance(meta, Mapping):
        return False
    if meta.get("kind") != "short_term":
        return False
    return validate_stored_lifetime(meta.get("lifetime"), field="dataset lifetime") is not None
