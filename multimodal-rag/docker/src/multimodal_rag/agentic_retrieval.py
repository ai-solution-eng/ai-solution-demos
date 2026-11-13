"""Agentic iterative retrieval: retrieve -> judge -> rewrite (opt-in, fail-open).

The plain retrieval path answers one query with one search.  This module adds an
OPT-IN loop around it: search, ask a judge whether the hits are enough, and when
they are not, search again with a query the judge rewrote.  Nothing here is
enabled by default -- callers pass agentic=True (see MultimodalRAG.aretrieve,
the MCP search_dataset tool and the REST search endpoints).

THE ONE RULE THAT MATTERS
-------------------------
The judge is an external, fallible component (usually an LLM over the network).
A judge that raises, times out, returns None, returns a string, returns a dict
with the wrong keys, or keeps proposing the same rewrite must NEVER fail the
retrieval.  Every one of those cases STOPS the loop and returns the results
already collected (fail open) with a stop_reason that says what happened.  The
loop is also bounded three ways -- rounds, total searches and a repeated-rewrite
guard -- so it cannot spin, and every judge call carries an ALWAYS-POSITIVE
timeout (a zero, negative, missing or unparseable judge_timeout_s means the
default, never "wait forever").

The non-agentic path is untouched: run_agentic_retrieval is only called when a
caller explicitly opts in, and MultimodalRAG.aretrieve delegates straight to its
pre-existing single-pass implementation otherwise.

INJECTABILITY
-------------
search_fn and judge_fn are plain callables (sync or async), so the whole loop is
testable offline with fakes -- no model endpoint, no network, no clock
dependence beyond time.monotonic for the trace's elapsed_s.
build_chat_judge is the only place that touches a model, and it accepts an
already-constructed chat model object (it imports nothing model-related), so
this module imports fine on a deployment with no model configured.

Public surface::

    AgenticConfig            (max_rounds, max_total_searches, min_round_gain, judge_timeout_s)
    AgenticTrace             (rounds, queries, new_doc_ids, judge_verdicts, stop_reason, elapsed_s)
    run_agentic_retrieval(query, search_fn, judge_fn, config=None) -> (results, trace)
    run_agentic_entries(query, search_fn, judge_fn, config=None) -> (entries, trace)
    build_chat_judge(chat_model) -> judge | None
    build_judge_prompt(query, results) -> str
    parse_judge_verdict(raw) -> dict | None
    doc_identity(doc) -> str
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import math
import re
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from multimodal_rag.utils.logging_utils import logging

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_JUDGE_TIMEOUT_S",
    "JUDGE_PROMPT",
    "MAX_AGENTIC_ROUNDS",
    "MAX_AGENTIC_SEARCHES",
    "MAX_JUDGE_TIMEOUT_S",
    "MIN_JUDGE_TIMEOUT_S",
    "STOP_REASONS",
    "AgenticConfig",
    "AgenticTrace",
    "build_chat_judge",
    "build_judge_prompt",
    "coerce_agentic_config",
    "doc_identity",
    "parse_judge_verdict",
    "query_text",
    "run_agentic_entries",
    "run_agentic_retrieval",
]

# Hard ceilings.  A caller-supplied config is clamped into these, so a typo (or
# a hostile request body) cannot turn one search into an unbounded fan-out of
# LLM calls.
MAX_AGENTIC_ROUNDS = 8
MAX_AGENTIC_SEARCHES = 16

# Judge timeout bounds.  The timeout is ALWAYS bounded: a non-positive, missing or
# unparseable value means "use the default", never "wait forever" -- otherwise a
# config file that serialises an absent value as 0 (or a caller passing 0 to mean
# "no limit") would hand the loop an unbounded wait on a hanging judge.
MIN_JUDGE_TIMEOUT_S = 0.1
MAX_JUDGE_TIMEOUT_S = 300.0
DEFAULT_JUDGE_TIMEOUT_S = 10.0

# How much retrieved context the default judge prompt carries.  Bounded so the
# judge call is cheap and a huge page cannot blow the model's context.
_JUDGE_RESULT_LIMIT = 8
_JUDGE_TEXT_LIMIT = 400

# Every value AgenticTrace.stop_reason can take.  Documented here (and asserted
# in the tests) so callers can branch on the reason instead of guessing from a
# free-form string.
STOP_REASONS = (
    "sufficient",  # judge: the collected results answer the query
    "max_rounds",  # round budget exhausted
    "max_searches",  # total-search budget exhausted
    "no_gain",  # the last round added <= min_round_gain new doc ids
    "no_rewrite",  # judge said "not sufficient" but gave no usable rewrite
    "repeated_rewrite",  # judge proposed a query that was already searched
    "judge_timeout",  # judge exceeded judge_timeout_s
    "judge_error",  # judge raised
    "invalid_verdict",  # judge returned None / unparseable garbage
    "search_error",  # a FOLLOW-UP search raised (round 1 errors propagate)
    "judge_unavailable",  # opt-in requested but no judge could be resolved
    "no_query_text",  # nothing to judge (media-only query) -- single pass
    "error_fallback",  # the hook's safety net caught an internal error
)

JUDGE_PROMPT = (
    "You are a retrieval judge. Decide whether the retrieved passages below are "
    "ENOUGH to answer the question. Do not answer the question itself.\n\n"
    "Reply with ONLY one JSON object, no prose and no code fences:\n"
    '{"sufficient": true|false, "missing": "<one line: what is missing>", '
    '"rewrite": "<one line: a better search query>"}\n'
    'When "sufficient" is true, "rewrite" may be an empty string.'
)


# ---------------------------------------------------------------------------
# Config + trace
# ---------------------------------------------------------------------------


@dataclass
class AgenticConfig:
    """Bounds for one agentic retrieval loop.

    Defaults are deliberately small: two rounds and four searches is enough for
    one rewrite to help, and cheap enough that opting in does not double a
    search's latency in the common case.  Every field is clamped by
    :meth:`normalized` before use, so out-of-range or wrong-typed values
    degrade to the default instead of raising.
    """

    max_rounds: int = 2
    max_total_searches: int = 4
    min_round_gain: int = 0
    judge_timeout_s: float = DEFAULT_JUDGE_TIMEOUT_S

    def normalized(self) -> AgenticConfig:
        """Return a copy with every field clamped into its supported range.

        judge_timeout_s is ALWAYS a bounded positive number after this call: a
        zero, negative, missing (None) or unparseable value resolves to the
        default 10 s, and a positive value below the floor is raised to
        MIN_JUDGE_TIMEOUT_S.  There is no value that means "no timeout".
        """
        return AgenticConfig(
            max_rounds=_clamp_int(self.max_rounds, 1, MAX_AGENTIC_ROUNDS, 2),
            max_total_searches=_clamp_int(self.max_total_searches, 1, MAX_AGENTIC_SEARCHES, 4),
            min_round_gain=_clamp_int(self.min_round_gain, 0, 1000, 0),
            judge_timeout_s=_bounded_judge_timeout(self.judge_timeout_s),
        )


@dataclass
class AgenticTrace:
    """What the loop did, for logs / metadata["_agentic"] / REST responses.

    rounds          number of searches actually executed (>= 1).
    queries         the queries issued, in order (round 1 is the original).
    new_doc_ids     doc ids that a REWRITE round added (round 1 establishes the
                    baseline and contributes none), in discovery order.
    judge_verdicts  one entry per judge call: the normalized verdict, or
                    {"error": <stop_reason>} when the judge failed.
    stop_reason     one of STOP_REASONS.
    elapsed_s       wall time of the whole loop, seconds.
    """

    rounds: int = 0
    queries: list[str] = field(default_factory=list)
    new_doc_ids: list[str] = field(default_factory=list)
    judge_verdicts: list[dict[str, Any]] = field(default_factory=list)
    stop_reason: str = "max_rounds"
    elapsed_s: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        """JSON-safe snapshot (no tuples, no objects) for metadata / responses."""
        return {
            "rounds": int(self.rounds),
            "queries": list(self.queries),
            "new_doc_ids": list(self.new_doc_ids),
            "judge_verdicts": [dict(v) for v in self.judge_verdicts],
            "stop_reason": str(self.stop_reason),
            "elapsed_s": round(float(self.elapsed_s), 6),
        }


def _clamp_int(value: Any, low: int, high: int, default: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


def _bounded_judge_timeout(value: Any) -> float:
    """Coerce *value* to a POSITIVE, bounded judge timeout.

    Non-positive, missing (None), NaN/inf or unparseable input means "use the
    default" -- NEVER "no timeout".  A positive value is clamped into
    [MIN_JUDGE_TIMEOUT_S, MAX_JUDGE_TIMEOUT_S], so even a caller that asks for
    0.001 s gets a real (tiny) bound rather than an unbounded wait.
    """
    try:
        number = float(value)
    except (TypeError, ValueError):
        return DEFAULT_JUDGE_TIMEOUT_S
    if not math.isfinite(number) or number <= 0.0:
        return DEFAULT_JUDGE_TIMEOUT_S
    return max(MIN_JUDGE_TIMEOUT_S, min(number, MAX_JUDGE_TIMEOUT_S))


_AGENTIC_CONFIG_KEYS = ("max_rounds", "max_total_searches", "min_round_gain", "judge_timeout_s")


def coerce_agentic_config(config: AgenticConfig | dict[str, Any] | None) -> AgenticConfig:
    """Normalize whatever a caller passed as agentic_config into a config.

    Accepts an :class:`AgenticConfig`, a dict of its fields (unknown keys
    are logged and ignored -- judge_fn is a hook-level key, not a config field),
    or None.  Never raises: anything unusable degrades to the defaults.
    """
    if config is None:
        return AgenticConfig().normalized()
    if isinstance(config, AgenticConfig):
        return config.normalized()
    if isinstance(config, dict):
        unknown = sorted(set(config) - set(_AGENTIC_CONFIG_KEYS) - {"judge_fn"})
        if unknown:
            logger.warning("agentic_config: ignoring unknown key(s) %s", ", ".join(unknown))
        known = {k: config[k] for k in _AGENTIC_CONFIG_KEYS if k in config}
        return AgenticConfig(**known).normalized()
    logger.warning(
        "agentic_config: expected AgenticConfig or dict, got %s -- using defaults",
        type(config).__name__,
    )
    return AgenticConfig().normalized()


# ---------------------------------------------------------------------------
# Query / document helpers (total: none of these raise on odd input)
# ---------------------------------------------------------------------------


def query_text(query: Any) -> str:
    """The judge/rewrite text view of a query (media-only queries yield "")."""
    if isinstance(query, str):
        return query
    if isinstance(query, dict):
        text = query.get("text")
        return text if isinstance(text, str) else ""
    return "" if query is None else str(query)


def _digest(text: str) -> str:
    return hashlib.md5(text.encode("utf-8", "replace")).hexdigest()[:16]


def doc_identity(doc: Any) -> str:
    """A stable, human-readable identity for one result document.

    Used to decide whether a round added anything NEW.  Precedence: an explicit
    point id, then the twin-identity key (source/page/chunk/time-window -- the
    same key the dedup pass uses), then the text itself, then a stable repr.
    Formatted-entry dicts ({"content": <doc>, ...}, as produced by
    DatasetManager.search and the federated REST search) recurse into content
    and are dataset-qualified when the entry carries a dataset label.
    Total by construction: never raises, always returns a string.
    """
    if isinstance(doc, dict):
        inner = doc.get("content")
        if isinstance(inner, (dict, str)):
            prefix = doc.get("dataset")
            identity = doc_identity(inner)
            return f"{prefix}/{identity}" if isinstance(prefix, str) and prefix else identity
        point_id = doc.get("_point_id") or doc.get("id")
        if point_id:
            return f"id:{point_id}"
        source = doc.get("source") or doc.get("original_source")
        if source:
            page = doc.get("page")
            chunk = doc.get("chunk_index")
            start = doc.get("timestamp_start")
            end = doc.get("timestamp_end")
            if start is None and end is None:
                return f"{source}#p{page}#c{chunk}"
            return f"{source}#p{page}#c{chunk}#t{start}-{end}"
        text = doc.get("text")
        if isinstance(text, str):
            return "text:" + _digest(text)
        return "obj:" + _digest(_stable_repr(doc))
    if isinstance(doc, str):
        return "text:" + _digest(doc)
    content = getattr(doc, "page_content", None)
    if isinstance(content, str):
        metadata = getattr(doc, "metadata", None)
        if isinstance(metadata, dict):
            return doc_identity({**metadata, "text": content})
        return "text:" + _digest(content)
    return "obj:" + _digest(_stable_repr(doc))


def _stable_repr(value: Any) -> str:
    try:
        return json.dumps(value, sort_keys=True, default=str)
    except Exception:
        return repr(value)


def _doc_text(doc: Any) -> str:
    """Best-effort text view of a document for the judge prompt."""
    if isinstance(doc, str):
        return doc
    if isinstance(doc, dict):
        text = doc.get("text")
        if isinstance(text, str):
            return text
        inner = doc.get("content")
        if isinstance(inner, (str, dict)):
            return _doc_text(inner)
        return _stable_repr({k: v for k, v in doc.items() if k != "image"})[:_JUDGE_TEXT_LIMIT]
    content = getattr(doc, "page_content", None)
    if isinstance(content, str):
        return content
    return "" if doc is None else str(doc)


def _as_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _split_pair(item: Any) -> tuple[Any, float]:
    """Accept (doc, score) pairs or bare docs."""
    if isinstance(item, (tuple, list)) and len(item) == 2:
        return item[0], _as_float(item[1])
    return item, 0.0


# ---------------------------------------------------------------------------
# Judge plumbing
# ---------------------------------------------------------------------------


def build_judge_prompt(query: str, results: Sequence[Any]) -> str:
    """Render the default judge prompt for query over results.

    Bounded on purpose: at most _JUDGE_RESULT_LIMIT passages, each cut to
    _JUDGE_TEXT_LIMIT characters, so a large result set cannot turn the judge
    call into a context-window problem.
    """
    parts = [JUDGE_PROMPT, "", f"Question: {query}", "", "Retrieved passages:"]
    shown = 0
    for item in list(results)[:_JUDGE_RESULT_LIMIT]:
        doc, _score = _split_pair(item)
        text = _doc_text(doc).strip()
        if not text:
            continue
        shown += 1
        parts.append(f"[{shown}] {text[:_JUDGE_TEXT_LIMIT]}")
    if shown == 0:
        parts.append("(no passages retrieved)")
    return "\n".join(parts)


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in ("true", "yes", "y", "1", "sufficient")
    return False


def _chat_text(response: Any) -> str:
    """Extract the assistant text from an SDK response, a dict, or a string."""
    if isinstance(response, str):
        return response
    choices = getattr(response, "choices", None)
    if choices:
        try:
            content = choices[0].message.content
        except Exception:
            content = None
        if isinstance(content, str):
            return content
    output_text = getattr(response, "output_text", None)
    if isinstance(output_text, str):
        return output_text
    if isinstance(response, dict):
        inner = response.get("choices")
        if isinstance(inner, list) and inner:
            first = inner[0]
            message = first.get("message") if isinstance(first, dict) else None
            if isinstance(message, dict) and isinstance(message.get("content"), str):
                return message["content"]
        for key in ("content", "text", "output_text"):
            if isinstance(response.get(key), str):
                return response[key]
    return ""


def parse_judge_verdict(raw: Any) -> dict[str, Any] | None:
    """Normalize a judge reply into {"sufficient", "missing", "rewrite"}.

    Tolerant by design -- a judge is usually an LLM, so the reply may arrive as
    a mapping, a JSON string, JSON wrapped in prose or code fences, or a raw SDK
    response object.  Returns None when nothing usable can be extracted; the
    loop treats None as STOP (fail open).  Never raises.
    """
    payload: Any = raw
    if not isinstance(payload, dict):
        if not isinstance(payload, str):
            payload = _chat_text(payload)
        if not isinstance(payload, str):
            return None
        text = payload.strip()
        if not text:
            return None
        parsed: Any = None
        try:
            parsed = json.loads(text)
        except (TypeError, ValueError):
            match = re.search(r"\{.*\}", text, re.DOTALL)
            if match is not None:
                try:
                    parsed = json.loads(match.group(0))
                except (TypeError, ValueError):
                    parsed = None
        if not isinstance(parsed, dict):
            return None
        payload = parsed

    if not {"sufficient", "missing", "rewrite"} & set(payload):
        return None
    missing = payload.get("missing")
    rewrite = payload.get("rewrite")
    return {
        "sufficient": _truthy(payload.get("sufficient")),
        "missing": missing.strip() if isinstance(missing, str) else "",
        "rewrite": rewrite.strip() if isinstance(rewrite, str) else "",
    }


def build_chat_judge(chat_model: Any) -> Callable[[str, Sequence[Any]], Any] | None:
    """Wrap a configured chat model as a judge, or return None if unusable.

    The model object is passed in (never imported or built here), which keeps
    this module importable on a deployment with no model configured and lets
    tests inject a fake with the same one-method surface.  The judge is an async
    callable; a model whose chat call raises propagates the exception so the
    loop can record stop_reason="judge_error" and fail open.
    """
    if chat_model is None:
        return None
    call = getattr(chat_model, "llm_async_chat_function_call", None)
    if not callable(call):
        logger.warning(
            "agentic retrieval: configured chat model has no llm_async_chat_function_call -- no judge available"
        )
        return None

    async def _judge(query: str, results: Sequence[Any]) -> dict[str, Any] | None:
        prompt = build_judge_prompt(query, results)
        response = await call([{"role": "user", "content": prompt}])
        return parse_judge_verdict(response)

    return _judge


# ---------------------------------------------------------------------------
# The loop
# ---------------------------------------------------------------------------


def _is_async_callable(fn: Any) -> bool:
    if inspect.iscoroutinefunction(fn):
        return True
    # A callable OBJECT whose __call__ is async: inspect only unwraps bound
    # methods, so look through the instance's type explicitly.
    return inspect.iscoroutinefunction(type(fn).__call__)


async def _invoke(fn: Any, *args: Any) -> Any:
    """Call fn whether it is sync or async, without blocking the event loop.

    A plain sync callable is run in a worker thread (so a slow judge can be
    cancelled by the timeout); a callable that merely RETURNS an awaitable (a
    lambda wrapping an async call) is awaited after the thread returns.
    """
    if _is_async_callable(fn):
        return await fn(*args)
    outcome = await asyncio.to_thread(fn, *args)
    if inspect.isawaitable(outcome):
        return await outcome
    return outcome


def _normalize_results(raw: Any) -> list[tuple[Any, float]]:
    """Coerce whatever a search function returned into [(doc, score)]."""
    if raw is None or isinstance(raw, (str, bytes, dict)):
        return []
    try:
        items = list(raw)
    except TypeError:
        return []
    normalized: list[tuple[Any, float]] = []
    for item in items:
        normalized.append(_split_pair(item))
    return normalized


async def _ask_judge(
    judge_fn: Any,
    query_text: str,
    results: list[tuple[Any, float]],
    timeout_s: float,
) -> tuple[dict[str, Any] | None, str | None]:
    """Run the judge under an ALWAYS-BOUNDED timeout.  Returns (verdict, error_stop_reason).

    The timeout is re-bounded here (not just in AgenticConfig) so a caller that
    reaches the loop with 0 / negative / garbage still cannot make the judge
    wait unbounded: those values mean the default, never "no limit".
    """
    bounded_timeout = _bounded_judge_timeout(timeout_s)
    call = _invoke(judge_fn, query_text, results)
    try:
        raw = await asyncio.wait_for(call, timeout=bounded_timeout)
    except TimeoutError:
        logger.warning("agentic retrieval: judge timed out after %.1fs -- stopping (fail open)", bounded_timeout)
        return None, "judge_timeout"
    except Exception:
        logger.warning("agentic retrieval: judge raised -- stopping (fail open)", exc_info=True)
        return None, "judge_error"
    verdict = parse_judge_verdict(raw)
    if verdict is None:
        logger.warning(
            "agentic retrieval: judge returned an unusable verdict (%s) -- stopping (fail open)",
            type(raw).__name__,
        )
        return None, "invalid_verdict"
    return verdict, None


def _merge_new(
    results: list[tuple[Any, float]],
    seen: set[str],
    round_results: list[tuple[Any, float]],
) -> list[str]:
    """Append the docs of round_results not seen before; return their ids."""
    gained: list[str] = []
    for doc, score in round_results:
        identity = doc_identity(doc)
        if identity in seen:
            continue
        seen.add(identity)
        results.append((doc, score))
        gained.append(identity)
    return gained


async def run_agentic_retrieval(
    query: Any,
    search_fn: Callable[[Any], Any],
    judge_fn: Callable[[str, Sequence[Any]], Any],
    config: AgenticConfig | dict[str, Any] | None = None,
) -> tuple[list[tuple[Any, float]], AgenticTrace]:
    """Run the retrieve -> judge -> rewrite loop and return the collected results.

    search_fn(query) may be sync or async and must return [(doc, score)] (any
    iterable of pairs; odd shapes are tolerated and treated as empty).
    judge_fn(query_text, results) may be sync or async and must return
    {"sufficient": bool, "missing": str, "rewrite": str} -- but any shape is
    tolerated: a raised judge, a timeout, None, an unparseable string or a dict
    without the expected keys all STOP the loop and return what has been
    collected so far (stop_reason says which).  A repeated rewrite also stops
    it, so the loop always terminates.

    Round 1 is the caller's query verbatim.  Rewrite rounds are issued as the
    rewritten STRING (the caller's search_fn decides how to fold that back into
    a multimodal query dict).  Results accumulate across rounds in discovery
    order and are deduped by :func:`doc_identity`.

    The only exception this function propagates is a failure of the FIRST
    search: at that point there are no "already collected" results to fail open
    to, and the caller's non-agentic behaviour is to surface that error.
    """
    cfg = coerce_agentic_config(config)
    started = time.monotonic()
    trace = AgenticTrace()
    judge_query = query_text(query)

    if not judge_query.strip():
        # Nothing for a judge to reason about (media-only query): one plain pass.
        trace.stop_reason = "no_query_text"
        try:
            raw = await _invoke(search_fn, query)
        finally:
            trace.elapsed_s = time.monotonic() - started
        trace.rounds = 1
        trace.queries.append(judge_query)
        return _normalize_results(raw), trace

    results: list[tuple[Any, float]] = []
    seen: set[str] = set()
    issued: list[str] = [judge_query]
    current: Any = query
    searches = 0
    trace.stop_reason = "max_rounds"

    while True:
        if searches >= cfg.max_total_searches:
            trace.stop_reason = "max_searches"
            break
        if trace.rounds >= cfg.max_rounds:
            trace.stop_reason = "max_rounds"
            break
        try:
            raw = await _invoke(search_fn, current)
        except Exception:
            if trace.rounds == 0:
                # No collected results to fail open to -- surface the caller's
                # own retrieval error exactly as the non-agentic path would.
                trace.elapsed_s = time.monotonic() - started
                raise
            logger.warning(
                "agentic retrieval: follow-up search failed -- returning %d collected result(s) (fail open)",
                len(results),
                exc_info=True,
            )
            trace.stop_reason = "search_error"
            break
        searches += 1
        trace.rounds += 1
        trace.queries.append(query_text(current))
        gained = _merge_new(results, seen, _normalize_results(raw))
        if trace.rounds > 1:
            trace.new_doc_ids.extend(gained)

        # Budgets are checked before the judge so an exhausted loop never pays
        # for an LLM call it cannot act on.
        if trace.rounds >= cfg.max_rounds:
            trace.stop_reason = "max_rounds"
            break
        if searches >= cfg.max_total_searches:
            trace.stop_reason = "max_searches"
            break
        if trace.rounds > 1 and len(gained) <= cfg.min_round_gain:
            trace.stop_reason = "no_gain"
            break

        verdict, error = await _ask_judge(judge_fn, judge_query, results, cfg.judge_timeout_s)
        if error is not None:
            trace.judge_verdicts.append({"error": error})
            trace.stop_reason = error
            break
        if verdict is None:  # pragma: no cover - _ask_judge returns one or an error
            trace.stop_reason = "invalid_verdict"
            break
        trace.judge_verdicts.append(verdict)
        if verdict["sufficient"]:
            trace.stop_reason = "sufficient"
            break
        rewrite = verdict["rewrite"]
        if not rewrite:
            trace.stop_reason = "no_rewrite"
            break
        if rewrite in issued:
            trace.stop_reason = "repeated_rewrite"
            break
        issued.append(rewrite)
        current = rewrite

    trace.elapsed_s = time.monotonic() - started
    return results, trace


async def run_agentic_entries(
    query: Any,
    search_fn: Callable[[Any], Any],
    judge_fn: Callable[[str, Sequence[Any]], Any],
    config: AgenticConfig | dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], AgenticTrace]:
    """Agentic loop over a search function that returns FORMATTED entries.

    search_fn(q) returns [{"content": <doc>, "score": <float>, ...}] -- the
    shape DatasetManager.search and the federated REST search produce.  Entries
    travel through the loop as the documents (identity comes from content,
    dataset-qualified when the entry carries a dataset label), so the returned
    list is the accumulated, identity-deduped entry list in discovery order:
    every entry keeps the score/score_kind fields its own round produced.
    """
    if not callable(judge_fn):
        raise TypeError("run_agentic_entries requires a judge_fn callable")

    async def _search(q: Any) -> list[tuple[Any, float]]:
        raw = await _invoke(search_fn, q)
        if not isinstance(raw, list):
            return []
        pairs: list[tuple[Any, float]] = []
        for entry in raw:
            if isinstance(entry, dict) and "content" in entry:
                pairs.append((entry, _as_float(entry.get("score"))))
            else:
                pairs.append(_split_pair(entry))
        return pairs

    results, trace = await run_agentic_retrieval(query, _search, judge_fn, config)
    return [doc for doc, _score in results], trace
