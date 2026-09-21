"""Opt-in ingest webhooks (Wave-5 F2).

When ``RAG_WEBHOOK_URL`` is set, every completed ingest fires one small JSON
event to that URL::

    POST <RAG_WEBHOOK_URL>
    Content-Type: application/json
    X-RAG-Webhook-Secret: <RAG_WEBHOOK_SECRET>   (only when configured)

    {"dataset": "reports", "doc_count": 12, "status": "ok",
     "timestamp": "2026-09-14T10:00:00+00:00"}

Design rules (ratified scope):

* **Opt-in** — unset ``RAG_WEBHOOK_URL`` means zero behaviour: no request,
  no latency, no log line (the notify helper returns ``False`` immediately).
* **Failures are logged, never fatal** — a webhook error must not fail an
  ingest that already succeeded.  Every failure mode (bad URL, DNS, connect,
  timeout, non-2xx) is caught and logged at WARNING with no exception.
* **Timeout-capped** — the POST is bounded by ``RAG_WEBHOOK_TIMEOUT``
  (seconds, default 5.0; values < 0.1 are clamped) so a dead receiver cannot
  stall the ingest path.
* Dataset restore/import replays are muted (``muted()`` context): a restore
  is one logical ingest, not hundreds of batch events.

RAG-local module (not part of the pcai_utils hardlink mesh); stdlib only.
"""

import contextvars
import json
import os
import urllib.error
import urllib.request
from datetime import UTC, datetime

from multimodal_rag.utils.logging_utils import logging

logger = logging.getLogger(__name__)

WEBHOOK_URL_ENV = "RAG_WEBHOOK_URL"
WEBHOOK_SECRET_ENV = "RAG_WEBHOOK_SECRET"
WEBHOOK_TIMEOUT_ENV = "RAG_WEBHOOK_TIMEOUT"
WEBHOOK_TIMEOUT_DEFAULT = 5.0

# Set while a bulk replay (dataset restore/import) is in progress so the
# per-batch ingest calls inside it do not fire hundreds of webhook events.
_muted: contextvars.ContextVar[bool] = contextvars.ContextVar("rag_webhook_muted", default=False)


def webhook_url() -> str:
    """Configured webhook endpoint ('' = feature off). Re-read per call."""
    return os.environ.get(WEBHOOK_URL_ENV, "").strip()


def webhook_timeout() -> float:
    """POST timeout in seconds (``RAG_WEBHOOK_TIMEOUT``, clamped >= 0.1)."""
    try:
        t = float(os.environ.get(WEBHOOK_TIMEOUT_ENV, WEBHOOK_TIMEOUT_DEFAULT))
    except (TypeError, ValueError):
        return WEBHOOK_TIMEOUT_DEFAULT
    return max(0.1, t)


class muted:
    """Context manager: suppress webhook events inside (dataset restores)."""

    def __enter__(self):
        self._token = _muted.set(True)
        return self

    def __exit__(self, *exc):
        _muted.reset(self._token)
        return False


def notify_ingest(dataset: str, doc_count: int, status: str = "ok") -> bool:
    """POST one ingest-completed event. Returns True when delivered.

    Never raises: any failure is logged (WARNING) and reported as False, so
    callers can fire-and-forget after a successful ingest.
    """
    url = webhook_url()
    if not url or _muted.get():
        return False
    payload = json.dumps(
        {
            "dataset": dataset,
            "doc_count": doc_count,
            "status": status,
            "timestamp": datetime.now(UTC).isoformat(timespec="seconds"),
        }
    ).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    secret = os.environ.get(WEBHOOK_SECRET_ENV, "").strip()
    if secret:
        headers["X-RAG-Webhook-Secret"] = secret
    req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=webhook_timeout()) as resp:
            ok = 200 <= getattr(resp, "status", 200) < 300
            if not ok:
                logger.warning(
                    "Ingest webhook to %s returned HTTP %s (dataset=%s status=%s)",
                    url,
                    getattr(resp, "status", "?"),
                    dataset,
                    status,
                )
            return ok
    except Exception as exc:  # logged-not-fatal by design
        logger.warning(
            "Ingest webhook to %s failed (dataset=%s status=%s): %s",
            url,
            dataset,
            status,
            exc,
        )
        return False
